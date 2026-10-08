"""Candidate search stages over a single analysis session."""

from __future__ import annotations

import math
from dataclasses import dataclass, field, replace

from core.models import ConstraintFailureKind, QualityCandidateResult, QualitySearchResult, QualitySearchStatus
from .bitrate import (
    refresh_candidate_predictions,
    reselect_from_candidates,
    rd_ambiguity_events,
    search_bitrate_candidates,
    predicted_output_size,
)
from .measurement import measure_size_only
from .runtime import search_tolerance_bps
from .sampling.planner import PlannedWindow, SamplePlan, fresh_validation_window, rank_scout_observations
from .session import AnalysisSession, emit_analysis_progress, sample_window


@dataclass(frozen=True)
class SearchSettings:
    measurement_fingerprint: str
    fingerprint: str
    sample_plan: SamplePlan
    required_ceiling: int
    tolerance: int


@dataclass
class SearchResult:
    candidates: list[QualityCandidateResult]
    selection: QualitySearchResult
    remaining_holdouts: list[PlannedWindow]
    remaining_reserves: list[PlannedWindow]
    search_history_scout_ids: set[str]
    terminal_result: QualitySearchResult | None = None
    holdout_min_vmaf: float | None = None
    refinement_records: list[dict[str, object]] = field(default_factory=list)
    adaptive_expansion_events: list[dict[str, object]] = field(default_factory=list)
    size_calibration_records: list[dict[str, object]] = field(default_factory=list)
    ambiguity_records: list[dict[str, object]] = field(default_factory=list)


def _exact_search_bounds(
    coarse_candidates: list[QualityCandidateResult],
    *,
    min_bitrate_bps: int,
    budget_bitrate_bps: int,
    ceiling_bps: int,
    min_vmaf: float,
) -> tuple[int, int, int]:
    passing = [candidate.video_bitrate_bps for candidate in coarse_candidates if candidate.min_vmaf >= min_vmaf]
    failing = [candidate.video_bitrate_bps for candidate in coarse_candidates if candidate.min_vmaf < min_vmaf]
    lower = max(failing) if failing else min_bitrate_bps
    if passing:
        seed = min(passing)
        return max(min_bitrate_bps, lower), seed, max(seed, ceiling_bps)
    return min_bitrate_bps, budget_bitrate_bps, ceiling_bps


def _predicted_threshold_bitrate(candidates: list[QualityCandidateResult], target: float) -> int | None:
    ordered = sorted(candidates, key=lambda value: value.video_bitrate_bps)
    for lower, upper in zip(ordered, ordered[1:]):
        if lower.min_vmaf < target <= upper.min_vmaf:
            delta = upper.min_vmaf - lower.min_vmaf
            if delta <= 1e-9:
                return upper.video_bitrate_bps
            fraction = (target - lower.min_vmaf) / delta
            return round(lower.video_bitrate_bps + fraction * (upper.video_bitrate_bps - lower.video_bitrate_bps))
    passing = [value for value in ordered if value.min_vmaf >= target]
    return passing[0].video_bitrate_bps if passing else None


def complete_candidates(
    candidates: list[QualityCandidateResult],
    window_count: int,
) -> list[QualityCandidateResult]:
    return [
        candidate
        for candidate in candidates
        if len(candidate.segment_vmaf) == window_count
        and math.isclose(
            candidate.min_vmaf,
            min(candidate.segment_vmaf),
            rel_tol=0.0,
            abs_tol=1e-9,
        )
    ]


def _initial_search(session: AnalysisSession, settings: SearchSettings) -> SearchResult:
    assert session.item.media_info is not None
    emit_analysis_progress(session.progress_callback, session.item, "searching")
    coarse_candidates: list[QualityCandidateResult] = []
    exact_min = session.budget.min_video_bitrate_bps
    exact_budget = session.budget.max_video_bitrate_bps
    exact_ceiling = settings.required_ceiling
    if not session.initial_candidates:
        coarse_candidates, _coarse_selected, _coarse_required = search_bitrate_candidates(
            evaluate=lambda bitrate: session.evaluate(bitrate, session.coarse_plan),
            min_bitrate_bps=session.budget.min_video_bitrate_bps,
            budget_bitrate_bps=session.budget.max_video_bitrate_bps,
            required_search_ceiling_bps=settings.required_ceiling,
            min_vmaf=float(session.item.options.min_vmaf),
            max_candidates=session.profile.coarse_max_candidates,
            max_output_bytes=session.budget.max_output_bytes,
            tolerance_bps=settings.tolerance,
        )
        exact_min, exact_budget, exact_ceiling = _exact_search_bounds(
            coarse_candidates,
            min_bitrate_bps=session.budget.min_video_bitrate_bps,
            budget_bitrate_bps=session.budget.max_video_bitrate_bps,
            ceiling_bps=settings.required_ceiling,
            min_vmaf=float(session.item.options.min_vmaf),
        )
    searched, _selected_bitrate, _required_bitrate = search_bitrate_candidates(
        evaluate=lambda bitrate: session.evaluate(bitrate, session.exact_plan),
        min_bitrate_bps=exact_min,
        budget_bitrate_bps=exact_budget,
        required_search_ceiling_bps=exact_ceiling,
        min_vmaf=float(session.item.options.min_vmaf),
        max_candidates=session.profile.exact_max_candidates,
        max_output_bytes=session.budget.max_output_bytes,
        initial_candidates=refresh_candidate_predictions(
            session.initial_candidates, session.budget, session.item.media_info.duration
        ),
        tolerance_bps=settings.tolerance,
        preferred_first_bitrate_bps=_predicted_threshold_bitrate(
            coarse_candidates, float(session.item.options.min_vmaf)
        ),
    )
    candidates = complete_candidates(searched, len(session.windows))
    if not candidates:
        candidates = searched

    selection = reselect_from_candidates(
        candidates,
        session.item,
        measurement_fingerprint=settings.measurement_fingerprint,
        fingerprint=settings.fingerprint,
    )
    return SearchResult(
        candidates=candidates,
        selection=selection,
        remaining_holdouts=list(settings.sample_plan.holdout_windows),
        remaining_reserves=list(settings.sample_plan.reserve_windows),
        search_history_scout_ids={window.scout_id for window in session.planned_search if window.scout_id is not None},
    )


def _calibrate_size(session: AnalysisSession, settings: SearchSettings, result: SearchResult) -> None:
    assert session.item.media_info is not None
    assert session.item.encoder_info is not None
    if result.selection.failure_kind == ConstraintFailureKind.SIZE_BLOCKED and result.remaining_reserves:
        required_candidate = next(
            (
                candidate
                for candidate in result.candidates
                if candidate.video_bitrate_bps == result.selection.selected_video_bitrate_bps
            ),
            None,
        )
        prediction = required_candidate.size_prediction if required_candidate else None
        if prediction is not None and required_candidate is not None:
            assert session.item.media_info is not None
            media_duration = session.item.media_info.duration
            central_bytes = predicted_output_size(
                prediction.mean_video_bitrate_bps,
                session.budget.audio_bitrate_bps,
                media_duration,
            )
            near_boundary = central_bytes <= session.budget.max_output_bytes < prediction.predicted_output_bytes
            if near_boundary:
                if session.scout_observations:
                    ranked = rank_scout_observations(session.scout_observations)
                    reserve_risks = {value.observation.window.id: value.risk.global_risk for value in ranked}
                    ordered_risks = sorted(reserve_risks.values())
                    median_risk = ordered_risks[len(ordered_risks) // 2]
                    representatives = sorted(
                        result.remaining_reserves,
                        key=lambda window: (
                            abs(reserve_risks.get(window.scout_id or "", 0.5) - median_risk),
                            abs(window.center_sec - media_duration / 2.0),
                        ),
                    )[:2]
                else:
                    representatives = sorted(
                        result.remaining_reserves,
                        key=lambda window: abs(window.center_sec - media_duration / 2.0),
                    )[:2]
                measured = measure_size_only(
                    session.ffmpeg_path,
                    session.item,
                    [sample_window(window) for window in representatives],
                    required_candidate.video_bitrate_bps,
                    session.temp_root,
                    session.log_file,
                    session.exact_plan,
                    cancel_check=session.cancel_check,
                    process_callback=session.process_callback,
                )
                required_candidate.observed_window_bitrates.extend(measured)
                required_candidate.size_prediction = None
                result.size_calibration_records.extend(
                    {
                        "window_id": window.id,
                        "observed_video_bitrate_bps": bitrate,
                    }
                    for window, bitrate in zip(representatives, measured)
                )
                result.candidates = refresh_candidate_predictions(
                    result.candidates, session.budget, session.item.media_info.duration
                )
                result.selection = reselect_from_candidates(
                    result.candidates,
                    session.item,
                    measurement_fingerprint=settings.measurement_fingerprint,
                    fingerprint=settings.fingerprint,
                )


def _expand_near_threshold(session: AnalysisSession, settings: SearchSettings, result: SearchResult) -> None:
    assert session.item.media_info is not None
    assert session.item.encoder_info is not None
    if (
        result.selection.success
        and result.selection.min_vmaf is not None
        and result.selection.min_vmaf - float(session.item.options.min_vmaf) < session.profile.quality_confidence_band
        and result.remaining_reserves
    ):
        expanded = max(
            result.remaining_reserves,
            key=lambda window: (len(window.reasons), -window.start_sec),
        )
        result.remaining_reserves.remove(expanded)
        promoted = replace(
            expanded,
            id=f"search:adaptive:{expanded.id}",
            reasons=tuple(dict.fromkeys((*expanded.reasons, "adaptive_near_threshold"))),
        )
        session.planned_search.append(promoted)
        if promoted.scout_id is not None:
            result.search_history_scout_ids.add(promoted.scout_id)
        result.adaptive_expansion_events.append(
            {
                "reason": "quality_score_near_threshold",
                "window_id": promoted.id,
                "selected_score": result.selection.min_vmaf,
            }
        )
        session.reset_search_windows()
        expanded_candidates, _, _ = search_bitrate_candidates(
            evaluate=lambda bitrate: session.evaluate(bitrate, session.exact_plan),
            min_bitrate_bps=result.selection.selected_video_bitrate_bps,
            budget_bitrate_bps=max(result.selection.selected_video_bitrate_bps, session.budget.max_video_bitrate_bps),
            required_search_ceiling_bps=settings.required_ceiling,
            min_vmaf=float(session.item.options.min_vmaf),
            max_candidates=session.profile.exact_max_candidates,
            max_output_bytes=session.budget.max_output_bytes,
            tolerance_bps=settings.tolerance,
        )
        result.candidates = complete_candidates(expanded_candidates, len(session.windows)) or expanded_candidates
        result.selection = reselect_from_candidates(
            result.candidates,
            session.item,
            measurement_fingerprint=settings.measurement_fingerprint,
            fingerprint=settings.fingerprint,
        )


def _verify_holdouts(
    session: AnalysisSession,
    bitrate_bps: int,
    holdouts: list[PlannedWindow],
    *,
    refinement_round: int,
) -> tuple[list[PlannedWindow], list[float]]:
    failed: list[PlannedWindow] = []
    scores: list[float] = []
    for holdout_index, holdout in enumerate(holdouts):
        emit_analysis_progress(
            session.progress_callback,
            session.item,
            "holdout_verification",
            holdout_index=holdout_index + 1,
            holdout_count=len(holdouts),
            refinement_round=refinement_round,
            candidate_bitrate_bps=bitrate_bps,
        )
        result = session.evaluate_planned_subset(bitrate_bps, [holdout])
        score = result.min_vmaf
        scores.append(score)
        passed = score >= float(session.item.options.min_vmaf)
        session.log_file.write(
            f"holdout={holdout.id} bitrate={bitrate_bps} VMAF={score:.3f} result={'PASS' if passed else 'FAIL'}\n"
        )
        session.log_file.flush()
        if not passed:
            failed.append(holdout)
    return failed, scores


def _refine_holdouts(session: AnalysisSession, settings: SearchSettings, result: SearchResult) -> None:
    assert session.item.media_info is not None
    assert session.item.encoder_info is not None
    refinement_round = 0
    final_holdout_scores: list[float] = []
    while result.selection.success and result.remaining_holdouts:
        failed, final_holdout_scores = _verify_holdouts(
            session,
            result.selection.selected_video_bitrate_bps,
            result.remaining_holdouts,
            refinement_round=refinement_round,
        )
        result.holdout_min_vmaf = min(final_holdout_scores) if final_holdout_scores else None
        if not failed:
            break
        if refinement_round >= session.profile.max_refinement_rounds:
            result.terminal_result = replace(
                result.selection,
                status=QualitySearchStatus.FAILED,
                reason=("Holdout verification still failed after the configured refinement limit."),
            )
            break
        refinement_round += 1
        emit_analysis_progress(
            session.progress_callback,
            session.item,
            "refining",
            refinement_round=refinement_round,
            refinement_limit=session.profile.max_refinement_rounds,
            promoted_window_count=len(failed),
        )
        result.refinement_records.append(
            {
                "round": refinement_round,
                "starting_bitrate_bps": result.selection.selected_video_bitrate_bps,
                "promoted_window_ids": [window.id for window in failed],
                "failed_vmaf": [
                    score for window, score in zip(result.remaining_holdouts, final_holdout_scores) if window in failed
                ],
            }
        )
        session.planned_search.extend(
            replace(
                window,
                id=f"search:promoted:{window.id}",
                reasons=tuple(dict.fromkeys((*window.reasons, "failed_holdout_promoted"))),
            )
            for window in failed
        )
        result.search_history_scout_ids.update(window.scout_id for window in failed if window.scout_id is not None)
        result.remaining_holdouts = [window for window in result.remaining_holdouts if window not in failed]
        if any("unscouted_validation" in window.reasons for window in failed):
            occupied = [
                (window.start_sec, window.duration_sec)
                for window in (*session.planned_search, *result.remaining_holdouts, *result.remaining_reserves)
            ]
            occupied.extend((value.window.start_sec, value.window.duration_sec) for value in session.scout_observations)
            fresh_blind = fresh_validation_window(
                session.item.media_info.duration, session.profile.sample_duration_sec, occupied,
                identity=f"holdout:unscouted:{refinement_round}",
            )
            if fresh_blind is None:
                result.terminal_result = replace(
                    result.selection, status=QualitySearchStatus.FAILED,
                    reason="Holdout refinement exhausted unscouted validation space.",
                )
                break
            result.remaining_holdouts.append(fresh_blind)
            result.refinement_records[-1]["fresh_unscouted_holdout_id"] = fresh_blind.id
        if not result.remaining_holdouts and result.remaining_reserves:
            reserve = result.remaining_reserves.pop(0)
            fresh = replace(
                reserve,
                id=f"holdout:fresh:{reserve.id}",
                reasons=tuple(dict.fromkeys((*reserve.reasons, "fresh_reserve_holdout"))),
            )
            if fresh.scout_id not in result.search_history_scout_ids:
                result.remaining_holdouts.append(fresh)
                result.refinement_records[-1]["fresh_replacement_holdout_ids"] = [fresh.id]
        if not result.remaining_holdouts:
            result.terminal_result = replace(
                result.selection,
                status=QualitySearchStatus.FAILED,
                reason=(
                    "Holdout refinement exhausted independent reserve windows; normal-confidence success is not valid."
                ),
            )
            break
        session.reset_search_windows()
        refined, _refined_selected, _refined_required = search_bitrate_candidates(
            evaluate=lambda bitrate: session.evaluate(bitrate, session.exact_plan),
            min_bitrate_bps=result.selection.selected_video_bitrate_bps,
            budget_bitrate_bps=max(
                result.selection.selected_video_bitrate_bps,
                session.budget.max_video_bitrate_bps,
            ),
            required_search_ceiling_bps=settings.required_ceiling,
            min_vmaf=float(session.item.options.min_vmaf),
            max_candidates=session.profile.exact_max_candidates,
            max_output_bytes=session.budget.max_output_bytes,
            tolerance_bps=settings.tolerance,
        )
        result.candidates = complete_candidates(refined, len(session.windows))
        result.selection = reselect_from_candidates(
            result.candidates or refined,
            session.item,
            measurement_fingerprint=settings.measurement_fingerprint,
            fingerprint=settings.fingerprint,
        )
        if not result.selection.success:
            if result.selection.failure_kind == ConstraintFailureKind.SIZE_BLOCKED:
                result.terminal_result = result.selection
            else:
                result.terminal_result = QualitySearchResult(
                    status=QualitySearchStatus.FAILED,
                    encoder_name=session.item.encoder_info.encoder_name,
                    backend=session.item.encoder_info.backend,
                    candidates=result.candidates or refined,
                    measurement_fingerprint=settings.measurement_fingerprint,
                    fingerprint=settings.fingerprint,
                    max_output_bytes=session.budget.max_output_bytes,
                    reason="Promoted holdout windows could not reach the VMAF target.",
                )
            break
    if not result.remaining_holdouts and settings.sample_plan.whole_video:
        result.holdout_min_vmaf = None


def _resolve_ambiguity(session: AnalysisSession, settings: SearchSettings, result: SearchResult) -> None:
    assert session.item.media_info is not None
    assert session.item.encoder_info is not None
    result.ambiguity_records = rd_ambiguity_events(result.candidates)
    if result.ambiguity_records:
        decision_point = min(
            result.candidates,
            key=lambda candidate: abs(candidate.min_vmaf - float(session.item.options.min_vmaf)),
        )
        repeated = session.evaluate(decision_point.video_bitrate_bps, session.exact_plan, force_remeasure=True)
        result.candidates = [
            repeated if candidate.video_bitrate_bps == repeated.video_bitrate_bps else candidate
            for candidate in result.candidates
        ]
        still_ambiguous = rd_ambiguity_events(result.candidates)
        for event in result.ambiguity_records:
            event["reevaluated_bitrate_bps"] = repeated.video_bitrate_bps
            event["still_ambiguous"] = bool(still_ambiguous)
        result.candidates = [replace(candidate, rd_ambiguous=True) for candidate in result.candidates]
        result.selection = reselect_from_candidates(
            result.candidates,
            session.item,
            measurement_fingerprint=settings.measurement_fingerprint,
            fingerprint=settings.fingerprint,
        )


def run_search(
    session: AnalysisSession,
    sample_plan: SamplePlan,
    *,
    measurement_fingerprint: str,
    fingerprint: str,
) -> SearchResult:
    assert session.item.media_info is not None
    configured_max = int(session.item.options.max_video_kbps) * 1_000
    required_ceiling = max(session.item.media_info.video_bitrate_bps, session.budget.max_video_bitrate_bps)
    if configured_max > 0:
        required_ceiling = min(required_ceiling, configured_max)
    settings = SearchSettings(
        measurement_fingerprint=measurement_fingerprint,
        fingerprint=fingerprint,
        sample_plan=sample_plan,
        required_ceiling=required_ceiling,
        tolerance=search_tolerance_bps(
            required_ceiling,
            min_bps=session.profile.min_search_tolerance_bps,
            ratio=session.profile.search_tolerance_ratio,
        ),
    )
    result = _initial_search(session, settings)
    _calibrate_size(session, settings, result)
    _expand_near_threshold(session, settings, result)
    _refine_holdouts(session, settings, result)
    verified_bitrate = result.selection.selected_video_bitrate_bps
    _resolve_ambiguity(session, settings, result)
    if (result.terminal_result is None and result.selection.success and result.remaining_holdouts
            and result.selection.selected_video_bitrate_bps != verified_bitrate):
        failed, scores = _verify_holdouts(session, result.selection.selected_video_bitrate_bps,
                                         result.remaining_holdouts, refinement_round=len(result.refinement_records))
        result.holdout_min_vmaf = min(scores)
        if failed:
            result.terminal_result = replace(result.selection, status=QualitySearchStatus.FAILED,
                                             reason="Final bitrate changed after ambiguity resolution and failed holdout validation.")
    session.log_file.write(
        f"selected_bitrate_bps={result.selection.selected_video_bitrate_bps}\n"
        f"search_min_vmaf={result.selection.min_vmaf}\n"
        f"holdout_min_vmaf={result.holdout_min_vmaf}\n"
        f"refinement_rounds={len(result.refinement_records)}\n"
    )
    session.log_file.flush()
    return result
