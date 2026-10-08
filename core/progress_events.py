"""Typed, backwards-compatible payload contract for progress callbacks.

Progress events intentionally remain ordinary dictionaries at runtime.  The
encoder, planner, and Smart analysis paths all add fields that are
useful to their consumers, so this is a ``total=False`` ``TypedDict`` rather
than a runtime event class.  Keeping the contract in ``core`` lets CLI and GUI
adapters share it without introducing a dependency on either entrypoint.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TypedDict

from core.models import QualitySearchResult, SegmentedAnalysisResult


class ProgressEvent(TypedDict, total=False):
    """Fields emitted by core progress callbacks and consumed by adapters.

    Unknown extension fields are still allowed by the runtime dictionaries;
    this type records the stable fields currently used by the application.
    Optional numeric fields include ``None`` because FFmpeg may emit a state
    before it has a measurable value (for example while cancelling).
    """

    stage: str
    phase: str
    state: str
    category: str
    message: str
    error: str
    file_path: str
    file_name: str
    output_path: str
    queue_item_id: str
    queue_backend: str
    queue_encoder: str
    current: int
    total: int
    percent: float | int | None
    pass_percent: float | int
    file_progress: float | int
    elapsed_sec: float | int | None
    duration_sec: float | int | None
    speed: str
    frame: int | None
    current_pass_index: int
    total_passes: int
    parallel: bool
    worker_count: int
    quality_search_result: QualitySearchResult
    segmented_analysis_result: SegmentedAnalysisResult
    measurement_budget: int
    target_video_bitrate_bps: int
    candidate_index: int
    candidate_limit: int
    candidate_bitrate_bps: int
    candidate_min_vmaf: float
    candidate_tier: str
    analysis_backend: str
    decode_backend: str
    vmaf_backend: str
    n_threads: int
    n_subsample: int
    reused_candidate_count: int
    scout_index: int
    scout_count: int
    scout_start_sec: float
    search_window_count: int
    holdout_window_count: int
    window_index: int
    window_count: int
    holdout_index: int
    holdout_count: int
    refinement_round: int
    refinement_limit: int
    promoted_window_count: int


ProgressCallback = Callable[[ProgressEvent], None]
