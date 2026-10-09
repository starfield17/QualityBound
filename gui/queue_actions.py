"""Qt-free actions that mutate a queue record.

``QueueTableModel`` owns Qt notifications and row lookup.  This module owns
the domain side effects behind the queue's decision dialogs so the behavior is
testable without constructing a Qt model or view.
"""

from __future__ import annotations

import copy
from pathlib import Path

from core.models import (
    DecisionActionCode,
    DecisionOption,
    EncodeOptions,
    QualitySearchResult,
    QualitySearchStatus,
    SegmentedAnalysisResult,
    SkipOrigin,
)
from core.smart import (
    accept_rejected_output,
    build_decision_options,
    delete_analysis_receipt,
    delete_segmented_analysis_receipt,
    discard_rejected_output,
    prepare_size_miss_retry,
    reselect_after_quality_decision,
)
from gui.queue_state import (
    QueueItemRecord,
    QueueItemStatus,
    QueueSourceDraft,
    reset_for_retry,
    unbind_for_retry,
    short_error,
)
from core.models import VideoFileItem


EDITABLE_ITEM_STATUSES = {
    QueueItemStatus.AWAITING_START,
    QueueItemStatus.QUEUED,
    QueueItemStatus.WAITING_ANALYSIS,
}


def can_edit_record(record: QueueItemRecord) -> bool:
    return record.status in EDITABLE_ITEM_STATUSES


def record_quality_result(record: QueueItemRecord) -> QualitySearchResult | SegmentedAnalysisResult | None:
    """Return whichever Smart result the item carries.

    Smart v1 stores a ``QualitySearchResult`` while Smart v2 stores a
    ``SegmentedAnalysisResult``.  Every decision path must accept both, so the
    lookup lives here instead of being re-derived from one field.
    """

    if record.plan_item is None:
        return None
    return record.plan_item.quality_search_result or record.plan_item.segmented_analysis_result


def _ensure_draft(record: QueueItemRecord) -> QueueSourceDraft:
    if record.draft is None:
        record.draft = QueueSourceDraft(VideoFileItem(record.source_path, Path(record.source_path.name)), None, record.media_info)
    return record.draft


def apply_options_to_record(
    record: QueueItemRecord,
    options: EncodeOptions,
    *,
    runtime_capabilities: dict | None = None,
) -> bool:
    """Save a per-source override; binding and validation happen at Start."""
    del runtime_capabilities  # Capabilities are selected with the start-time tools.
    if not can_edit_record(record):
        return False
    _ensure_draft(record).options_override = copy.deepcopy(options)
    unbind_for_retry(record)
    return True


def apply_output_dir_to_record(record: QueueItemRecord, output_dir: Path) -> bool:
    """Save an independent directory override without creating outputs."""
    if not can_edit_record(record):
        return False
    _ensure_draft(record).output_dir_override = output_dir.expanduser().resolve()
    unbind_for_retry(record)
    return True


def decision_options_for_record(record: QueueItemRecord) -> list[DecisionOption]:
    """Return local quality choices available for a needs-decision record."""

    if record.status != QueueItemStatus.NEEDS_DECISION:
        return []
    result = record.result
    if result is None or result.rejected_output_path is not None:
        return []
    quality = record_quality_result(record)
    return build_decision_options(quality) if quality is not None else []


def apply_quality_decision(record: QueueItemRecord, decision: DecisionOption) -> bool:
    """Apply a quality decision to ``record`` without emitting UI signals."""

    if record.status != QueueItemStatus.NEEDS_DECISION:
        return False
    quality = record_quality_result(record)
    if quality is None:
        return False

    if decision.action_code == DecisionActionCode.SKIP:
        if record.result is not None:
            record.result.needs_decision = False
            record.result.skipped = True
            record.result.skip_origin = SkipOrigin.SMART_ANALYSIS_DECISION
        record.status = QueueItemStatus.SKIPPED
        record.error_summary = quality.reason
        return True

    if decision.action_code == DecisionActionCode.REANALYZE:
        try:
            if quality.measurement_fingerprint:
                if isinstance(quality, SegmentedAnalysisResult):
                    delete_segmented_analysis_receipt(record.bound_job_snapshot.workdir, quality.measurement_fingerprint)
                else:
                    delete_analysis_receipt(record.bound_job_snapshot.workdir, quality.measurement_fingerprint)
        except (OSError, ValueError) as exc:
            record.error_summary = short_error(str(exc))
            return False
        record.bound_plan_item.quality_search_result = None
        record.bound_plan_item.segmented_analysis_result = None
        reset_for_retry(record)
        return True

    reselected = reselect_after_quality_decision(
        record.bound_job_snapshot.ffmpeg_path,
        record.bound_plan_item,
        quality,
        decision,
    )
    if isinstance(reselected, SegmentedAnalysisResult):
        record.bound_plan_item.segmented_analysis_result = reselected
        if reselected.success or decision.requires_analysis:
            reset_for_retry(record)
        else:
            if record.result is not None:
                record.result.segmented_analysis_result = reselected
                record.result.error_message = reselected.reason
            record.error_summary = reselected.reason
        return True
    record.bound_plan_item.quality_search_result = reselected
    if reselected.status == QualitySearchStatus.FOUND:
        record.bound_plan_item.target_video_bitrate_bps = reselected.selected_video_bitrate_bps
        reset_for_retry(record)
    elif decision.requires_analysis:
        reselected.fingerprint = ""
        reset_for_retry(record)
    else:
        if record.result is not None:
            record.result.quality_search_result = reselected
            record.result.error_message = reselected.reason
        record.error_summary = reselected.reason
    return True


def _size_miss_record(record: QueueItemRecord) -> bool:
    result = record.result
    return (
        record.status == QueueItemStatus.NEEDS_DECISION
        and result is not None
        and result.rejected_output_path is not None
    )


def accept_size_miss(record: QueueItemRecord) -> bool:
    """Publish the preserved output for a size miss."""

    if not _size_miss_record(record):
        return False
    result = record.result
    assert result is not None
    try:
        accept_rejected_output(record.bound_plan_item, result)
    except (OSError, ValueError) as exc:
        record.error_summary = short_error(str(exc))
        return False
    record.status = QueueItemStatus.DONE
    record.file_progress = 100.0
    record.error_summary = None
    return True


def discard_size_miss(record: QueueItemRecord) -> bool:
    """Delete the preserved output and mark the item skipped."""

    if not _size_miss_record(record):
        return False
    result = record.result
    assert result is not None
    try:
        discard_rejected_output(record.bound_plan_item, result)
    except (OSError, ValueError) as exc:
        record.error_summary = short_error(str(exc))
        return False
    record.status = QueueItemStatus.SKIPPED
    record.error_summary = result.error_message
    return True


def retry_size_miss(record: QueueItemRecord) -> bool:
    """Prepare a preserved size miss for another encode attempt."""

    if not _size_miss_record(record):
        return False
    result = record.result
    assert result is not None
    try:
        prepare_size_miss_retry(record.bound_plan_item, result)
    except ValueError as exc:
        record.error_summary = short_error(str(exc))
        return False
    reset_for_retry(record)
    return True
