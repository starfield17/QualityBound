"""One analysis call's measurement resources, progress and backend fallbacks."""

from __future__ import annotations

import hashlib
import subprocess
import time
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Callable, TextIO, cast

from core.models import AnalysisProfileSettings, EncodePlanItem, OperationCancelledError, QualityCandidateResult, VmafBackend
from core.progress_events import ProgressCallback, ProgressEvent
from .bitrate import SmartBitrateBudget
from .runtime import (
    SOURCE_DECODE_SOFTWARE,
    AnalysisExecutionPlan,
    AnalysisTier,
    cpu_vmaf_plan,
    legacy_loopback_plan,
    software_source_plan,
)
from .measurement import (
    SampleWindow,
    WindowMeasurement,
    SmartCommandError,
    build_reference,
    log_timing,
    run_logged,
    score_candidate,
    score_candidate_loopback,
)
from .sampling.planner import PlannedWindow, ScoutObservation, rank_scout_observations
from .size_prediction import predict_size_distribution


def _hardest_window_index(candidate: QualityCandidateResult) -> int:
    if not candidate.segment_vmaf:
        return 0
    return min(range(len(candidate.segment_vmaf)), key=lambda index: candidate.segment_vmaf[index])


def _window_order(window_count: int, hardest_index: int) -> list[int]:
    if window_count <= 1:
        return [0] if window_count == 1 else []
    hardest = min(max(0, hardest_index), window_count - 1)
    return [hardest, *[index for index in range(window_count) if index != hardest]]


def sample_window(window: PlannedWindow) -> SampleWindow:
    return SampleWindow(window.start_sec, window.duration_sec)


def emit_analysis_progress(
    callback: ProgressCallback | None,
    item: EncodePlanItem,
    state: str,
    **values: object,
) -> None:
    if callback is None:
        return
    event = {
        "stage": "analysis",
        "state": state,
        "file_name": item.source_path.name,
        "file_path": str(item.source_path),
        **values,
    }
    callback(cast(ProgressEvent, event))


@dataclass
class AnalysisSession:
    ffmpeg_path: Path
    item: EncodePlanItem
    workdir: Path
    temp_root: Path
    log_file: TextIO
    budget: SmartBitrateBudget
    exact_plan: AnalysisExecutionPlan
    coarse_plan: AnalysisExecutionPlan
    planned_search: list[PlannedWindow]
    scout_observations: list[ScoutObservation]
    initial_candidates: list[QualityCandidateResult]
    progress_callback: ProgressCallback | None = None
    cancel_check: Callable[[], bool] | None = None
    process_callback: Callable[[subprocess.Popen[str] | None], None] | None = None
    windows: list[SampleWindow] = field(init=False)
    references: list[Path] = field(default_factory=list, init=False)
    reference_cache: dict[tuple[SampleWindow, str], Path] = field(default_factory=dict, init=False)
    measurement_cache: dict[str, WindowMeasurement] = field(default_factory=dict, init=False)
    hardest_window: int = field(default=0, init=False)
    candidate_indexes: dict[AnalysisTier, int] = field(
        default_factory=lambda: {AnalysisTier.COARSE: 0, AnalysisTier.EXACT: 0},
        init=False,
    )

    def __post_init__(self) -> None:
        self.windows = [sample_window(window) for window in self.planned_search]

    @property
    def profile(self) -> AnalysisProfileSettings:
        return self.item.options.analysis_settings

    def reset_search_windows(self) -> None:
        self.windows = [sample_window(window) for window in self.planned_search]
        self.references = []
        self.hardest_window = 0
        self.candidate_indexes[AnalysisTier.EXACT] = 0

    def ensure_references(self, plan: AnalysisExecutionPlan) -> AnalysisExecutionPlan:
        if self.references:
            return plan
        active_plan = plan
        references: list[Path] = []
        for index, window in enumerate(self.windows):
            if self.cancel_check is not None and self.cancel_check():
                raise OperationCancelledError("Smart analysis cancelled.")
            key = (window, active_plan.source_decode_acceleration)
            cached = self.reference_cache.get(key)
            if cached is not None and cached.is_file():
                references.append(cached)
                log_timing(self.log_file, f"reference cache hit: {cached.name}")
                continue
            identity = f"{self.item.source_path.resolve()}:{window.start_sec:.9f}:{window.duration_sec:.9f}:{key[1]}"
            reference_path = self.temp_root / f"reference-{hashlib.sha256(identity.encode()).hexdigest()[:24]}.mkv"
            extract_started = time.perf_counter()
            command = build_reference(
                self.ffmpeg_path,
                self.item,
                window,
                reference_path,
                decode_acceleration=active_plan.source_decode_acceleration,
            )
            try:
                run_logged(
                    command,
                    self.log_file,
                    cancel_check=self.cancel_check,
                    process_callback=self.process_callback,
                    phase="reference extraction",
                )
            except SmartCommandError:
                if active_plan.source_decode_acceleration == SOURCE_DECODE_SOFTWARE:
                    raise
                reason = f"{active_plan.source_decode_acceleration} source decode failed; retrying with software"
                log_timing(self.log_file, reason)
                active_plan = software_source_plan(active_plan, reason=reason)
                self.exact_plan = software_source_plan(self.exact_plan, reason=reason)
                self.coarse_plan = software_source_plan(self.coarse_plan, reason=reason)
                run_logged(
                    build_reference(
                        self.ffmpeg_path,
                        self.item,
                        window,
                        reference_path,
                        decode_acceleration=SOURCE_DECODE_SOFTWARE,
                    ),
                    self.log_file,
                    cancel_check=self.cancel_check,
                    process_callback=self.process_callback,
                    phase="reference extraction",
                )
            log_timing(
                self.log_file,
                f"reference extraction #{index + 1}: {time.perf_counter() - extract_started:.2f}s",
            )
            references.append(reference_path)
            self.reference_cache[(window, active_plan.source_decode_acceleration)] = reference_path
        self.references = references
        return active_plan

    def evaluate(
        self, bitrate_bps: int, plan: AnalysisExecutionPlan, *, force_remeasure: bool = False,
    ) -> QualityCandidateResult:
        active_plan = self.ensure_references(plan)
        if plan.tier == AnalysisTier.EXACT:
            self.exact_plan = active_plan
        else:
            self.coarse_plan = active_plan
        self.candidate_indexes[plan.tier] += 1
        candidate_index = self.candidate_indexes[plan.tier]
        limit = (
            self.profile.exact_max_candidates if plan.tier == AnalysisTier.EXACT else self.profile.coarse_max_candidates
        )
        if self.progress_callback is not None:
            self.progress_callback(
                {
                    "stage": "analysis",
                    "state": "analyzing",
                    "candidate_index": candidate_index,
                    "candidate_limit": limit,
                    "candidate_bitrate_bps": bitrate_bps,
                    "candidate_tier": plan.tier.value,
                    "analysis_backend": plan.analysis_backend,
                    "decode_backend": plan.source_decode_acceleration,
                    "vmaf_backend": plan.vmaf_backend.value,
                    "n_threads": plan.vmaf_threads,
                    "n_subsample": plan.vmaf_subsample,
                    "reused_candidate_count": len(self.initial_candidates),
                    "file_name": self.item.source_path.name,
                    "file_path": str(self.item.source_path),
                }
            )
        order = _window_order(len(self.windows), self.hardest_window)
        try:
            if active_plan.use_loopback:
                try:
                    result = score_candidate_loopback(
                        self.ffmpeg_path,
                        self.item,
                        self.windows,
                        bitrate_bps,
                        self.temp_root,
                        self.log_file,
                        active_plan,
                        audio_bitrate_bps=self.budget.audio_bitrate_bps,
                        source_bytes=self.budget.source_bytes,
                        cancel_check=self.cancel_check,
                        process_callback=self.process_callback,
                        min_vmaf_target=float(self.item.options.min_vmaf),
                        window_order=order,
                        measurement_cache=self.measurement_cache,
                        force_remeasure=force_remeasure,
                    )
                except (SmartCommandError, RuntimeError) as exc:
                    reason = f"loopback scoring failed; using legacy FFV1 path ({exc})"
                    log_timing(self.log_file, reason)
                    active_plan = legacy_loopback_plan(active_plan, reason=reason)
                    self.exact_plan = legacy_loopback_plan(self.exact_plan, reason=reason)
                    self.coarse_plan = legacy_loopback_plan(self.coarse_plan, reason=reason)
                    active_plan = self.ensure_references(active_plan)
                    result = score_candidate(
                        self.ffmpeg_path,
                        self.item,
                        self.references,
                        bitrate_bps,
                        self.temp_root,
                        self.workdir,
                        self.log_file,
                        window_durations_sec=[window.duration_sec for window in self.windows],
                        audio_bitrate_bps=self.budget.audio_bitrate_bps,
                        source_bytes=self.budget.source_bytes,
                        cancel_check=self.cancel_check,
                        process_callback=self.process_callback,
                        plan=active_plan,
                        min_vmaf_target=float(self.item.options.min_vmaf),
                        window_order=order,
                        measurement_cache=self.measurement_cache,
                        force_remeasure=force_remeasure,
                    )
            else:
                result = score_candidate(
                    self.ffmpeg_path,
                    self.item,
                    self.references,
                    bitrate_bps,
                    self.temp_root,
                    self.workdir,
                    self.log_file,
                    window_durations_sec=[window.duration_sec for window in self.windows],
                    audio_bitrate_bps=self.budget.audio_bitrate_bps,
                    source_bytes=self.budget.source_bytes,
                    cancel_check=self.cancel_check,
                    process_callback=self.process_callback,
                    plan=active_plan,
                    min_vmaf_target=float(self.item.options.min_vmaf),
                    window_order=order,
                    measurement_cache=self.measurement_cache,
                    force_remeasure=force_remeasure,
                )
        except SmartCommandError:
            if active_plan.vmaf_backend != VmafBackend.CUDA:
                raise
            reason = "CUDA VMAF failed; retrying with CPU libvmaf"
            log_timing(self.log_file, reason)
            active_plan = cpu_vmaf_plan(active_plan, reason=reason)
            self.exact_plan = cpu_vmaf_plan(self.exact_plan, reason=reason)
            self.coarse_plan = cpu_vmaf_plan(self.coarse_plan, reason=reason)
            result = score_candidate(
                self.ffmpeg_path,
                self.item,
                self.references,
                bitrate_bps,
                self.temp_root,
                self.workdir,
                self.log_file,
                window_durations_sec=[window.duration_sec for window in self.windows],
                audio_bitrate_bps=self.budget.audio_bitrate_bps,
                source_bytes=self.budget.source_bytes,
                cancel_check=self.cancel_check,
                process_callback=self.process_callback,
                plan=active_plan,
                min_vmaf_target=float(self.item.options.min_vmaf),
                window_order=order,
                measurement_cache=self.measurement_cache,
                force_remeasure=force_remeasure,
            )
        if len(result.segment_vmaf) == len(self.windows):
            self.hardest_window = _hardest_window_index(result)
        if (
            len(result.observed_window_bitrates) == len(self.planned_search)
            and self.windows == [sample_window(window) for window in self.planned_search]
            and self.scout_observations
            and all(window.scout_id is not None for window in self.planned_search)
        ):
            assert self.item.media_info is not None
            ranked_risks = rank_scout_observations(self.scout_observations)
            risk_by_id = {value.observation.window.id: value.risk.global_risk for value in ranked_risks}
            timeline_risks = [value.risk.global_risk for value in ranked_risks]
            sample_risks = [risk_by_id.get(window.scout_id or "", 0.5) for window in self.planned_search]
            prediction = predict_size_distribution(
                requested_bitrate_bps=result.video_bitrate_bps,
                observed_window_bitrates=result.observed_window_bitrates,
                duration_sec=self.item.media_info.duration,
                audio_bitrate_bps=self.budget.audio_bitrate_bps,
                source_bytes=self.budget.source_bytes,
                sample_risks=sample_risks,
                timeline_risks=timeline_risks,
            )
            result = replace(
                result,
                observed_video_bitrate_bps=prediction.mean_video_bitrate_bps,
                predicted_output_bytes=prediction.predicted_output_bytes,
                predicted_output_ratio=prediction.predicted_output_ratio,
                size_prediction=prediction,
            )
        if self.progress_callback is not None:
            self.progress_callback(
                {
                    "stage": "analysis",
                    "state": "candidate_finished",
                    "candidate_index": candidate_index,
                    "candidate_limit": limit,
                    "candidate_bitrate_bps": bitrate_bps,
                    "candidate_min_vmaf": result.min_vmaf,
                    "candidate_tier": plan.tier.value,
                    "reused_candidate_count": len(self.initial_candidates),
                    "file_name": self.item.source_path.name,
                    "file_path": str(self.item.source_path),
                }
            )
        return result

    def evaluate_planned_subset(
        self,
        bitrate_bps: int,
        subset: list[PlannedWindow],
    ) -> QualityCandidateResult:
        saved_windows = self.windows
        saved_references = self.references
        saved_hardest = self.hardest_window
        self.candidate_indexes[AnalysisTier.EXACT] = 0
        self.windows = [sample_window(window) for window in subset]
        self.references = []
        self.hardest_window = 0
        try:
            return self.evaluate(bitrate_bps, self.exact_plan)
        finally:
            self.windows = saved_windows
            self.references = saved_references
            self.hardest_window = saved_hardest
